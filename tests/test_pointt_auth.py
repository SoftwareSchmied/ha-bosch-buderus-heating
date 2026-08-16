from __future__ import annotations

import hashlib
from base64 import urlsafe_b64encode
from urllib.parse import parse_qs, urlparse

import aiohttp
import pytest
from aiohttp import web

from custom_components.bosch_buderus_heating.pointt import (
    AuthenticationError,
    AuthorizationCodeConsumed,
    AuthorizationGrant,
    Brand,
    InvalidPayload,
    OAuthClient,
    OAuthFlow,
    OAuthRedirectError,
    OAuthStateMismatch,
    RefreshTokenRejected,
    ServiceUnavailable,
    UnexpectedHttpStatus,
    create_pkce_context,
    parse_redirect_url,
)

pytestmark = pytest.mark.usefixtures("socket_enabled")


def test_pkce_context_is_fresh_and_uses_s256() -> None:
    first = create_pkce_context(now=12.0)
    second = create_pkce_context(now=12.0)

    expected = (
        urlsafe_b64encode(hashlib.sha256(first.code_verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    assert first.code_challenge == expected
    assert first.state != second.state
    assert first.nonce != second.nonce
    assert first.code_verifier != second.code_verifier
    assert first.created_at == 12.0


@pytest.mark.parametrize(
    ("brand", "redirect_uri", "style_id"),
    [
        (Brand.BOSCH, "com.bosch.tt.dashtt.pointt://app/login", "tt_bsch"),
        (Brand.BUDERUS, "com.buderus.tt.dashtt://app/login", "tt_bud"),
    ],
)
def test_authorization_url_contains_brand_and_correlation_values(
    brand: Brand, redirect_uri: str, style_id: str
) -> None:
    flow = OAuthFlow.create(brand, now=100.0)
    query = parse_qs(urlparse(flow.authorization_url).query)

    assert query["redirect_uri"] == [redirect_uri]
    assert query["style_id"] == [style_id]
    assert query["state"] == [flow.context.state]
    assert query["nonce"] == [flow.context.nonce]
    assert query["code_challenge_method"] == ["S256"]
    assert "offline_access" in query["scope"][0]


def test_flow_accepts_full_redirect_once() -> None:
    flow = OAuthFlow.create(Brand.BUDERUS, now=100.0)
    redirect = f"{Brand.BUDERUS.redirect_uri}?code=one-time&state={flow.context.state}"

    grant = flow.consume_redirect(redirect, now=101.0)

    assert grant.code == "one-time"
    assert grant.code_verifier == flow.context.code_verifier
    with pytest.raises(AuthorizationCodeConsumed):
        flow.consume_redirect(redirect, now=102.0)


def test_flow_rejects_expired_or_invalid_redirects() -> None:
    flow = OAuthFlow.create(Brand.BOSCH, now=100.0)
    valid = f"{Brand.BOSCH.redirect_uri}?code=code&state={flow.context.state}"
    with pytest.raises(OAuthRedirectError, match="expired"):
        flow.consume_redirect(valid, now=701.0)

    with pytest.raises(OAuthRedirectError, match="URI"):
        parse_redirect_url(
            Brand.BOSCH, "https://example.test/callback", expected_state="x"
        )
    with pytest.raises(OAuthStateMismatch):
        parse_redirect_url(
            Brand.BOSCH,
            f"{Brand.BOSCH.redirect_uri}?code=code&state=wrong",
            expected_state="right",
        )
    with pytest.raises(OAuthRedirectError, match="OAuth error"):
        parse_redirect_url(
            Brand.BOSCH,
            f"{Brand.BOSCH.redirect_uri}?error=denied&state=right",
            expected_state="right",
        )
    with pytest.raises(OAuthRedirectError, match="code"):
        parse_redirect_url(
            Brand.BOSCH,
            f"{Brand.BOSCH.redirect_uri}?state=right",
            expected_state="right",
        )


async def _serve(handler: web.RequestHandler) -> tuple[web.AppRunner, str]:
    app = web.Application()
    app.router.add_post("/token", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}/token"


async def test_oauth_client_exchanges_and_refreshes_tokens() -> None:
    requests: list[dict[str, str]] = []

    async def handler(request: web.Request) -> web.Response:
        requests.append(dict(await request.post()))
        return web.json_response(
            {
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 3600,
                "token_type": "Bearer",
                "scope": "openid profile",
            }
        )

    runner, url = await _serve(handler)
    try:
        async with aiohttp.ClientSession() as session:
            client = OAuthClient(session, token_url=url, clock=lambda: 1000.0)
            tokens = await client.exchange_code(
                Brand.BOSCH, AuthorizationGrant("code", "verifier")
            )
            refreshed = await client.refresh("old-refresh")
    finally:
        await runner.cleanup()

    assert tokens.expires_at == 4600.0
    assert tokens.scope == ("openid", "profile")
    assert refreshed.refresh_token == "new-refresh"
    assert requests[0]["grant_type"] == "authorization_code"
    assert requests[0]["code_verifier"] == "verifier"
    assert requests[1]["grant_type"] == "refresh_token"


@pytest.mark.parametrize(
    ("status", "refresh", "exception"),
    [
        (400, False, AuthenticationError),
        (401, True, RefreshTokenRejected),
        (500, False, ServiceUnavailable),
        (418, False, UnexpectedHttpStatus),
    ],
)
async def test_oauth_client_maps_http_errors(
    status: int, refresh: bool, exception: type[Exception]
) -> None:
    async def handler(_request: web.Request) -> web.Response:
        return web.Response(status=status, text="sensitive body")

    runner, url = await _serve(handler)
    try:
        async with aiohttp.ClientSession() as session:
            client = OAuthClient(session, token_url=url)
            with pytest.raises(exception) as caught:
                if refresh:
                    await client.refresh("secret")
                else:
                    await client.exchange_code(
                        Brand.BOSCH, AuthorizationGrant("code", "verifier")
                    )
    finally:
        await runner.cleanup()

    assert "sensitive body" not in str(caught.value)


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"access_token": "token", "expires_in": True},
        {"access_token": "token", "expires_in": float("inf")},
        {"access_token": "token", "expires_in": 10, "refresh_token": 3},
        {"access_token": "token", "expires_in": 10, "token_type": 3},
        {"access_token": "token", "expires_in": 10, "scope": ["ok", 3]},
    ],
)
async def test_oauth_client_rejects_invalid_token_payloads(payload: object) -> None:
    async def handler(_request: web.Request) -> web.Response:
        return web.json_response(payload)

    runner, url = await _serve(handler)
    try:
        async with aiohttp.ClientSession() as session:
            with pytest.raises(InvalidPayload):
                await OAuthClient(session, token_url=url).refresh("secret")
    finally:
        await runner.cleanup()


async def test_oauth_client_rejects_malformed_json() -> None:
    async def handler(_request: web.Request) -> web.Response:
        return web.Response(text="not json", content_type="application/json")

    runner, url = await _serve(handler)
    try:
        async with aiohttp.ClientSession() as session:
            with pytest.raises(InvalidPayload, match="valid JSON"):
                await OAuthClient(session, token_url=url).refresh("secret")
    finally:
        await runner.cleanup()
