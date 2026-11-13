"""Exercise actual TLS and form bodies against synthetic token endpoints."""

import asyncio
import json
from contextlib import suppress
from dataclasses import replace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs

import pytest

from custom_components.bosch_buderus_heating import local_auth as auth
from custom_components.bosch_buderus_heating import local_transport as transport
from custom_components.bosch_buderus_heating.local_auth import (
    AuthProblem,
    AuthState,
    LabelCredentials,
    request_local_token,
)
from custom_components.bosch_buderus_heating.local_probe import (
    LocalEndpoint,
    TransportFailure,
)
from custom_components.bosch_buderus_heating.local_transport import (
    CertificateBinding,
    LocalTransportError,
    PortRole,
)
from tests.test_local_probe import CONTEXT
from tests.test_local_transport import certificates as certificates
from tests.test_local_transport import wire_reply

pytestmark = pytest.mark.usefixtures("socket_enabled")
LABEL = LabelCredentials("synthetic+login", "synthetic-Pass+with&chars")
PAYLOAD = {
    "access_token": "synthetic-created-token",
    "token_type": "Bearer",
    "scope": "open_api.read",
}


@pytest.fixture
async def auth_gateway(certificates, monkeypatch):
    servers, tasks = [], set()

    async def start(responder=None, *, certificate=0):
        requests, received = [], asyncio.Event()

        async def handle(reader, writer):
            task = asyncio.current_task()
            tasks.add(task)
            try:
                head = await reader.readuntil(b"\r\n\r\n")
                headers = dict(
                    line.decode().split(": ", 1)
                    for line in head.split(b"\r\n")[1:]
                    if line
                )
                body = await reader.readexactly(int(headers.get("Content-Length", "0")))
                requests.append((head, body))
                received.set()
                if responder:
                    await responder(head, body, writer)
                else:
                    writer.write(wire_reply(json.dumps(PAYLOAD).encode()))
                    await writer.drain()
            except asyncio.IncompleteReadError, ConnectionError:
                pass
            finally:
                writer.close()
                with suppress(OSError):
                    await writer.wait_closed()
                tasks.discard(task)

        server = await asyncio.start_server(
            handle, "127.0.0.1", 0, ssl=certificates[certificate][0]
        )
        servers.append(server)
        monkeypatch.setattr(
            LocalEndpoint, "auth_port", server.sockets[0].getsockname()[1]
        )
        monkeypatch.setattr(
            transport, "_resolve", AsyncMock(return_value=("127.0.0.1",))
        )
        binding = CertificateBinding(
            CONTEXT, PortRole.AUTHENTICATION, certificates[certificate][1]
        )
        return requests, received, binding

    yield start
    for server in servers:
        server.close()
        await server.wait_closed()
    for task in tuple(tasks):
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def request(binding, *, active=lambda: True):
    return await request_local_token(binding.context, LABEL, binding, is_current=active)


async def test_tls_form_success_never_sends_credentials_to_resource_port(
    auth_gateway, monkeypatch
):
    calls, _, binding = await auth_gateway()
    monkeypatch.setattr(LocalEndpoint, "resource_port", 1)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    outcome = await request(binding)
    assert outcome.state == AuthState.RECEIVED
    assert outcome.credentials.token == PAYLOAD["access_token"]
    assert outcome.credentials.reported_scope == ("open_api.read",)
    assert len(calls) == 1
    head, body = calls[0]
    assert head.startswith(b"POST /auth/token HTTP/1.1\r\n")
    assert b"Content-Type: application/x-www-form-urlencoded" in head
    assert b"Authorization:" not in head and b"Cookie:" not in head
    assert parse_qs(body.decode()) == {k: [v] for k, v in LABEL.form().items()}
    for value in (LABEL, outcome, outcome.credentials):
        assert LABEL.login not in repr(value) and LABEL.password not in repr(value)
        assert PAYLOAD["access_token"] not in repr(value)


@pytest.mark.parametrize(
    "status,problem",
    [
        (400, AuthProblem.INVALID_CREDENTIALS),
        (401, AuthProblem.UNAUTHORIZED),
        (403, AuthProblem.FORBIDDEN),
        (404, AuthProblem.NOT_FOUND),
        (412, AuthProblem.PHYSICAL_CONFIRMATION_REQUIRED),
        (415, AuthProblem.MEDIA_TYPE),
        (429, AuthProblem.RATE_LIMITED),
        (507, AuthProblem.TOKEN_DATABASE_FULL),
    ],
)
async def test_known_rejection_is_distinct_and_never_retried(
    auth_gateway, status, problem
):
    async def reply(head, body, writer):
        writer.write(
            wire_reply(
                b"private server error", status=status, headers={"Retry-After": "45"}
            )
        )
        await writer.drain()

    calls, _, binding = await auth_gateway(reply)
    result = await request(binding)
    assert result.state == AuthState.REJECTED and result.problem == problem
    assert result.retry_after_seconds == (45 if status == 429 else None)
    assert result.credentials is None and len(calls) == 1


@pytest.mark.parametrize("status", [201, 204, 301, 302, 307, 308, 500, 503])
async def test_ambiguous_response_and_redirect_do_not_repeat(auth_gateway, status):
    async def reply(head, body, writer):
        writer.write(
            wire_reply(
                json.dumps(PAYLOAD).encode(),
                status=status,
                headers={
                    "Location": "https://127.0.0.1:1/auth/token",
                    "Retry-After": "25",
                },
            )
        )
        await writer.drain()

    calls, _, binding = await auth_gateway(reply)
    result = await request(binding)
    assert result.state == AuthState.UNCERTAIN and result.credentials is None
    assert result.retry_after_seconds == (25 if status == 503 else None)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "body,headers",
    [
        (b"{", {}),
        (b"\xff", {}),
        (b"[]", {}),
        (b'{"access_token":"a","access_token":"b"}', {}),
        (json.dumps({**PAYLOAD, "scope": "unknown"}).encode(), {}),
        (json.dumps({**PAYLOAD, "access_token": "bad\ntoken"}).encode(), {}),
        (b"[" * 1200 + b"]" * 1200, {}),
        (json.dumps(PAYLOAD).encode()[:-1] + b',"extra":NaN}', {}),
        (json.dumps(PAYLOAD).encode()[:-1] + b',"extra":1e999}', {}),
        (
            json.dumps(PAYLOAD).encode()[:-1]
            + b',"extra":'
            + b"[" * 40
            + b"0"
            + b"]" * 40
            + b"}",
            {},
        ),
        (b"x" * (auth.MAX_AUTH_BODY + 1), {}),
        (json.dumps(PAYLOAD).encode(), {"Content-Type": "text/html"}),
        (
            json.dumps(PAYLOAD).encode(),
            {"Content-Type": "application/json; charset=latin-1"},
        ),
        (json.dumps(PAYLOAD).encode(), {"Content-Encoding": "gzip"}),
    ],
)
async def test_invalid_success_body_keeps_creation_uncertain(
    auth_gateway, body, headers
):
    async def reply(head, form, writer):
        writer.write(wire_reply(body, headers=headers))
        await writer.drain()

    calls, _, binding = await auth_gateway(reply)
    assert (await request(binding)).state == AuthState.UNCERTAIN
    assert len(calls) == 1


async def test_chunked_oversize_and_missing_body_are_uncertain(auth_gateway):
    async def reply(head, body, writer):
        writer.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            b"Transfer-Encoding: chunked\r\n\r\n"
        )
        chunk = b"x" * (auth.MAX_AUTH_BODY + 1)
        writer.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n0\r\n\r\n")
        await writer.drain()

    calls, _, binding = await auth_gateway(reply)
    assert (await request(binding)).state == AuthState.UNCERTAIN and len(calls) == 1


@pytest.mark.parametrize(
    "failure,problem",
    [
        (TransportFailure.DNS, AuthProblem.DNS),
        (TransportFailure.INTERFACE, AuthProblem.INTERFACE),
        (TransportFailure.TLS, AuthProblem.TLS),
        (TransportFailure.TIMEOUT, AuthProblem.TIMEOUT),
        (TransportFailure.CONNECT, AuthProblem.CONNECTION),
    ],
)
async def test_preflight_failure_is_not_sent(monkeypatch, failure, problem):
    monkeypatch.setattr(
        auth, "_inspect", AsyncMock(side_effect=LocalTransportError(failure))
    )
    pin = CertificateBinding(CONTEXT, PortRole.AUTHENTICATION, b"a" * 32)
    result = await request(pin)
    assert result.state == AuthState.NOT_SENT and result.problem == problem


async def test_certificate_change_with_same_key_blocks_label(
    auth_gateway, certificates
):
    calls, _, binding = await auth_gateway(certificate=1)
    result = await request(replace(binding, sha256=certificates[0][1]))
    assert result.problem == AuthProblem.CERTIFICATE_CHANGED and not calls


async def test_between_preflight_and_post_certificate_change_sends_no_form(
    auth_gateway, certificates, monkeypatch
):
    calls, _, binding = await auth_gateway()
    original = auth._inspect

    async def inspect(*args, **kwargs):
        observed = await original(*args, **kwargs)
        await auth_gateway(certificate=1)
        return observed

    monkeypatch.setattr(auth, "_inspect", inspect)
    result = await request(binding)
    assert result.state == AuthState.NOT_SENT and not calls
    assert result.problem == AuthProblem.CERTIFICATE_CHANGED


async def test_response_loss_timeout_and_cancel_never_repeat(auth_gateway, monkeypatch):
    async def wait(head, body, writer):
        await asyncio.Future()

    calls, received, binding = await auth_gateway(wait)
    monkeypatch.setattr(auth, "REQUEST_TIMEOUT", 1.0)
    assert (await request(binding)).state == AuthState.UNCERTAIN and len(calls) == 1
    received.clear()
    task = asyncio.create_task(request(binding))
    await received.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(calls) == 2


async def test_close_after_post_and_inactive_after_success(auth_gateway):
    async def close(head, body, writer):
        writer.close()

    calls, _, binding = await auth_gateway(close)
    assert (await request(binding)).state == AuthState.UNCERTAIN and len(calls) == 1
    active = True

    async def reply(head, body, writer):
        nonlocal active
        active = False
        writer.write(wire_reply(json.dumps(PAYLOAD).encode()))
        await writer.drain()

    calls, _, binding = await auth_gateway(reply)
    assert (await request(binding, active=lambda: active)).state == AuthState.RECEIVED
    assert len(calls) == 1
    assert (await request(binding, active=lambda: False)).state == AuthState.NOT_SENT
    assert len(calls) == 1


async def test_token_post_uses_the_shared_hardened_local_session(
    auth_gateway, monkeypatch
):
    calls, _, binding = await auth_gateway()
    sessions = []
    shared = transport.create_local_session

    def recording(**kwargs):
        session = shared(**kwargs)
        sessions.append((kwargs, session))
        return session

    monkeypatch.setattr(auth, "create_local_session", recording)
    assert (await request(binding)).state == AuthState.RECEIVED
    assert len(calls) == 1
    assert len(sessions) == 1
    kwargs, session = sessions[0]
    assert set(kwargs) == {"trace_configs"} and len(kwargs["trace_configs"]) == 1
    assert session.closed
    assert session.trust_env is False
    assert isinstance(session.cookie_jar, transport.aiohttp.DummyCookieJar)
    assert session.auto_decompress is False


@pytest.mark.parametrize(
    "login,password",
    [("", "a"), ("x" * 129, "a"), ("a\nb", "a"), ("a", "---"), ("a", "x ")],
)
def test_invalid_label_never_enters_transport(login, password):
    with pytest.raises(ValueError, match="invalid_label_credentials"):
        LabelCredentials(login, password)


async def test_wrong_role_or_context_rejected_before_any_request(auth_gateway):
    calls, _, binding = await auth_gateway()
    with pytest.raises(ValueError, match="authentication_binding_required"):
        await request(replace(binding, role=PortRole.RESOURCES))
    with pytest.raises(ValueError, match="authentication_binding_required"):
        await request_local_token(
            replace(CONTEXT, endpoint=LocalEndpoint("other.invalid")),
            LABEL,
            binding,
            is_current=lambda: True,
        )
    assert not calls
