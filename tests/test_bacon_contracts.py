"""Bacon boundary contracts and authenticated HTTP against a local server."""

import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import aiohttp
import pytest
from aiohttp import web

from custom_components.bosch_buderus_heating.bacon.client import BaconClient
from custom_components.bosch_buderus_heating.bacon.models import (
    MAX_BYTES,
    MAX_DEVICES,
    BaconError,
    account_key,
    device_key,
    device_list,
    identifier,
    object_payload,
    shadow,
    token_subject,
    topic,
)
from custom_components.bosch_buderus_heating.pointt import (
    AuthenticationError,
    RateLimited,
    ServiceUnavailable,
)
from tests.test_pointt_transport_and_client import serve

pytestmark = pytest.mark.usefixtures("socket_enabled")


def token(subject="account-A", signature="synthetic"):
    payload = base64.urlsafe_b64encode(json.dumps({"sub": subject}).encode())
    return "header." + payload.decode().rstrip("=") + "." + signature


def provider(value=None):
    return SimpleNamespace(get_access_token=AsyncMock(return_value=value or token()))


@pytest.mark.parametrize(
    "bad", ["", "a/b", "+", "#", "a b", "a\n", "ü", "a" * 129, 5, None]
)
def test_identifier_excludes_ambiguous_routing(bad):
    with pytest.raises(BaconError, match="identifier"):
        identifier(bad)


@pytest.mark.parametrize(
    "payload", [{}, None, ["a", "a"], ["/x"], ["a"] * (MAX_DEVICES + 1)]
)
def test_device_list_rejects_invalid_contract(payload):
    with pytest.raises(BaconError):
        device_list(payload)


def test_identity_separates_backends_and_accounts():
    assert device_list(["a", "b"]) == ("a", "b")
    assert device_list([]) == ()
    assert token_subject(token()) == "account-A"
    assert identifier("a_0:-.") == "a_0:-."
    assert len(account_key("account-A")) == 64
    assert device_key("account-A", "1") != device_key("account-B", "1")
    assert device_key("account-A", "1").startswith("bacon:")
    assert (
        topic("account-A", "1", "topics/info")
        == "users/account-A/devices/1/topics/info"
    )


@pytest.mark.parametrize(
    "bad",
    [
        "x",
        "a..b",
        "a.W10.b",
        "a.e30.b",
        "a.b.c.d",
        "a" * 32769,
        token("a/#"),
        token(None),
    ],
)
def test_invalid_token_does_not_leak_subject(bad):
    with pytest.raises(BaconError, match=r"^authentication$"):
        token_subject(bad)


@pytest.mark.parametrize(
    "bad",
    [
        b"[]",
        b"null",
        b"{",
        b"\xff",
        b'{"x":NaN}',
        b'{"x":Infinity}',
        b'{"x":-Infinity}',
        b'{"x":' + b"[" * 18 + b"1" + b"]" * 18 + b"}",
        b" " * (MAX_BYTES + 1),
    ],
)
def test_message_bounds_and_json_types(bad):
    with pytest.raises(BaconError, match="invalid_payload"):
        object_payload(bad)


def test_json_and_shadow_contracts():
    data = object_payload(
        '{"state":{"reported":{"mode":"on"}}, "version":0,"timestamp":0}'
    )
    state = shadow(data)
    assert state.reported == {"mode": "on"}
    assert state.desired == {}
    assert "mode" not in repr(state)


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", True),
        ("version", -1),
        ("version", "1"),
        ("timestamp", False),
        ("timestamp", -1),
        ("state", []),
        ("state", {"reported": []}),
        ("state", {"desired": 0}),
    ],
)
def test_invalid_shadow(field, value):
    data = {"state": {}, "version": 1, "timestamp": 1}
    data[field] = value
    with pytest.raises(BaconError, match="invalid_shadow"):
        shadow(data)


@pytest.mark.parametrize(
    "status,body,category",
    [
        (200, b"{}", "invalid_device_list"),
        (200, b"{", "invalid_device_list"),
        (200, b"[" + b" " * MAX_BYTES + b"]", "invalid_device_list"),
        (403, b"", "authentication"),
        (401, b"", "authentication"),
        (500, b"", "service_unavailable"),
        (302, b"", "service_unavailable"),
    ],
)
async def test_http_contract_errors(status, body, category):
    async def response(request):
        return web.Response(
            status=status, body=body, headers={"Location": "/forbidden"}
        )

    async with (
        serve([("GET", "/", response)]) as url,
        aiohttp.ClientSession() as session,
    ):
        tokens = provider()
        client = BaconClient(session, tokens, url=url)
        with pytest.raises(BaconError, match=category):
            await client.get_devices()
        assert tokens.get_access_token.await_count == (2 if status == 401 else 1)


async def test_http_refresh_is_bounded_and_uses_new_token():
    calls = []
    tokens = provider()
    tokens.get_access_token.side_effect = [token(), token(signature="renewed")]

    async def response(request):
        calls.append(request.headers["Authorization"])
        return (
            web.json_response(["serial-A", "serial-B"])
            if len(calls) > 1
            else web.Response(status=401)
        )

    async with (
        serve([("GET", "/", response)]) as url,
        aiohttp.ClientSession() as session,
    ):
        client = BaconClient(session, tokens, url=url)
        assert await client.get_devices() == ("serial-A", "serial-B")
        assert calls == ["Bearer " + token(), "Bearer " + token(signature="renewed")]
        tokens.get_access_token.assert_awaited_with(
            force_refresh=True, rejected_token=token()
        )


@pytest.mark.parametrize(
    "header,seconds", [("120", 120), ("2", 60), ("-3", 60), ("nan", 60), ("no", 60)]
)
async def test_http_rate_limit_prevents_another_request(header, seconds):
    calls = []

    async def response(request):
        calls.append(1)
        return web.Response(status=429, headers={"Retry-After": header})

    async with (
        serve([("GET", "/", response)]) as url,
        aiohttp.ClientSession() as session,
    ):
        client = BaconClient(session, provider(), url=url, clock=lambda: 100)
        # The server's limit first, then the local block of its pause (C49).
        for category in ("rate_limited", "paused"):
            with pytest.raises(BaconError, match=category) as exc:
                await client.get_devices()
            assert exc.value.retry_after == seconds
        assert calls == [1]


@pytest.mark.parametrize(
    "error,category",
    [
        (AuthenticationError("secret"), "authentication"),
        (RateLimited(120), "rate_limited"),
        (ServiceUnavailable(503), "connection"),
    ],
)
async def test_shared_token_provider_errors_remain_safe(error, category):
    tokens = provider()
    tokens.get_access_token.side_effect = error
    client = BaconClient(None, tokens)
    with pytest.raises(BaconError, match="^" + category + "$"):
        await client.access_token()


async def test_account_cannot_change_during_refresh():
    tokens = provider(token("account-B"))
    client = BaconClient(None, tokens)
    client.subject = "account-A"
    with pytest.raises(BaconError, match="account_changed"):
        await client.access_token()


async def test_connection_error_is_local():
    session = SimpleNamespace(
        get=lambda *a, **k: (_ for _ in ()).throw(aiohttp.ClientError("private"))
    )
    with pytest.raises(BaconError, match=r"^connection$"):
        await BaconClient(session, provider()).get_devices()
