from __future__ import annotations

import hashlib
import json
from base64 import urlsafe_b64encode
from urllib.parse import parse_qs, urlparse

import aiohttp
import pytest
from aiohttp import web

from custom_components.bosch_buderus_heating.pointt import (
    AuthenticationError,
    AuthorizationCodeConsumed,
    AuthorizationGrant,
    AuthTokens,
    Brand,
    InvalidPayload,
    OAuthClient,
    OAuthFlow,
    OAuthRedirectError,
    OAuthStateMismatch,
    RateLimited,
    RefreshTokenRejected,
    ServiceUnavailable,
    TokenManager,
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


@pytest.mark.parametrize("replacement", [None, "", "   "])
async def test_invalid_refresh_token_does_not_replace_or_persist_tokens(replacement):
    async def handler(request: web.Request) -> web.Response:
        return web.json_response(
            {
                "access_token": "new-access",
                "refresh_token": replacement,
                "expires_in": 3600,
            }
        )

    runner, url = await _serve(handler)
    try:
        async with aiohttp.ClientSession() as session:
            original = AuthTokens("old-access", "old-refresh", 0.0)
            persisted = []
            manager = TokenManager(
                OAuthClient(session, token_url=url), original, persisted.append
            )
            with pytest.raises(InvalidPayload):
                await manager.get_access_token()
            assert manager.tokens is original
            assert not persisted
    finally:
        await runner.cleanup()


async def test_missing_refresh_token_keeps_previous_token():
    async def handler(request: web.Request) -> web.Response:
        return web.json_response({"access_token": "new-access", "expires_in": 3600})

    runner, url = await _serve(handler)
    try:
        async with aiohttp.ClientSession() as session:
            tokens = await OAuthClient(session, token_url=url).refresh("old-refresh")
            assert tokens.refresh_token == "old-refresh"
    finally:
        await runner.cleanup()


@pytest.mark.parametrize("refresh", [False, True])
@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("120", 120.0),
        ("Thu, 01 Jan 1970 00:18:40 GMT", 120.0),
        ("invalid", None),
        ("inf", None),
        ("-2", 0.0),
        (None, None),
    ],
)
async def test_oauth_rate_limit_preserves_retry_after(refresh, header, expected):
    calls = 0

    async def handler(request: web.Request) -> web.Response:
        nonlocal calls
        calls += 1
        return web.Response(
            status=429,
            headers={"Retry-After": header} if header is not None else {},
            text="sensitive response body",
        )

    runner, url = await _serve(handler)
    try:
        async with aiohttp.ClientSession() as session:
            client = OAuthClient(session, token_url=url, clock=lambda: 1000.0)
            with pytest.raises(RateLimited) as caught:
                if refresh:
                    await client.refresh("refresh-token")
                else:
                    await client.exchange_code(
                        Brand.BUDERUS, AuthorizationGrant("code", "verifier")
                    )
            assert caught.value.retry_after == expected
            assert "sensitive" not in str(caught.value)
            assert calls == 1
    finally:
        await runner.cleanup()


async def _rejected_refresh(handler) -> RefreshTokenRejected:
    runner, url = await _serve(handler)
    try:
        async with aiohttp.ClientSession() as session:
            client = OAuthClient(session, token_url=url)
            with pytest.raises(RefreshTokenRejected) as caught:
                await client.refresh("secret-refresh-token-value")
    finally:
        await runner.cleanup()
    return caught.value


async def test_rejected_refresh_reports_sanitized_oauth_reason() -> None:
    async def handler(_request: web.Request) -> web.Response:
        return web.json_response(
            {"error": "invalid_grant", "error_description": "Token is not active"},
            status=400,
        )

    error = await _rejected_refresh(handler)
    assert error.rejection is not None
    assert error.rejection.http_status == 400
    assert error.rejection.oauth_error == "invalid_grant"
    assert error.rejection.oauth_error_description == "Token is not active"
    assert error.rejection.body_format == "json"
    assert error.rejection.content_type == "application/json"
    assert str(error) == (
        "Refresh token was rejected (HTTP 400, invalid_grant: Token is not active)"
    )
    assert "secret-refresh-token-value" not in str(error)


async def test_rejected_refresh_description_drops_secrets_and_accounts() -> None:
    opaque = "eyJhbGciOiJSUzI1NiJ9abcdefghijklmnop"

    async def handler(_request: web.Request) -> web.Response:
        return web.json_response(
            {
                "error": "invalid grant <script>",
                "error_description": (
                    f"Session of user person@example.com ended; token {opaque} "
                    "<b>revoked</b>" + " padding" * 40
                ),
            },
            status=401,
        )

    error = await _rejected_refresh(handler)
    assert error.rejection is not None
    description = error.rejection.oauth_error_description
    assert error.rejection.oauth_error == "other"
    assert description is not None
    assert len(description) <= 120
    assert "person" not in description
    assert "example.com" not in description
    assert opaque not in description
    assert "<" not in description
    assert "[redacted]" in description


@pytest.mark.parametrize(
    ("body", "content_type", "body_format"),
    [
        ("<html>Access denied by firewall</html>", "text/html", "non_json"),
        ("", None, "empty"),
        ('["unexpected"]', "application/json", "json_non_object"),
    ],
)
async def test_rejected_refresh_classifies_non_oauth_bodies(
    body: str, content_type: str | None, body_format: str
) -> None:
    async def handler(_request: web.Request) -> web.Response:
        if content_type is None:
            return web.Response(status=403)
        return web.Response(status=403, text=body, content_type=content_type)

    error = await _rejected_refresh(handler)
    assert error.rejection is not None
    assert error.rejection.http_status == 403
    assert error.rejection.oauth_error is None
    assert error.rejection.body_format == body_format
    if content_type is not None:
        assert error.rejection.content_type == content_type
    assert "firewall" not in str(error)
    assert f"no OAuth error code, {body_format} body" in str(error)


async def test_refresh_records_response_metadata_without_changing_equality() -> None:
    async def handler(_request: web.Request) -> web.Response:
        return web.json_response(
            {
                "access_token": "new-access",
                "expires_in": 300,
                "refresh_expires_in": 2592000,
            }
        )

    runner, url = await _serve(handler)
    try:
        async with aiohttp.ClientSession() as session:
            client = OAuthClient(session, token_url=url, clock=lambda: 1000.0)
            refreshed = await client.refresh("kept-refresh")
    finally:
        await runner.cleanup()

    assert refreshed.issued_at == 1000.0
    assert refreshed.refresh_expires_in == 2592000.0
    assert refreshed.refresh_token_returned is False
    assert refreshed.refresh_token == "kept-refresh"
    assert refreshed == AuthTokens("new-access", "kept-refresh", 1300.0)


async def test_rejected_refresh_ignores_unusual_content_and_descriptions() -> None:
    async def handler(_request: web.Request) -> web.Response:
        return web.Response(
            status=400,
            body=b'{"error": "invalid_grant", "error_description": 7}',
            headers={"Content-Type": "Application/JSON Weird; charset=utf-8"},
        )

    error = await _rejected_refresh(handler)
    assert error.rejection is not None
    assert error.rejection.content_type == "other"
    assert error.rejection.oauth_error == "invalid_grant"
    assert error.rejection.oauth_error_description is None
    assert str(error) == "Refresh token was rejected (HTTP 400, invalid_grant)"


@pytest.mark.parametrize("value", [-1, True, "3600", float("nan")])
async def test_invalid_refresh_lifetime_is_ignored(value: object) -> None:
    async def handler(_request: web.Request) -> web.Response:
        return web.Response(
            text=json.dumps(
                {"access_token": "a", "expires_in": 60, "refresh_expires_in": value}
            ),
            content_type="application/json",
        )

    runner, url = await _serve(handler)
    try:
        async with aiohttp.ClientSession() as session:
            refreshed = await OAuthClient(session, token_url=url).refresh("r")
    finally:
        await runner.cleanup()
    assert refreshed.refresh_expires_in is None
