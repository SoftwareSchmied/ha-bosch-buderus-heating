from __future__ import annotations

import asyncio
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
    OAuthScopeRejected,
    OAuthStateMismatch,
    RateLimited,
    RefreshRefused,
    RefreshTokenRejected,
    ServiceUnavailable,
    SignInUnavailable,
    TokenManager,
    UnexpectedHttpStatus,
    create_pkce_context,
    parse_redirect_url,
)
from custom_components.bosch_buderus_heating.pointt.const import (
    OAUTH_SCOPES,
    OAUTH_SCOPES_REDUCED,
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
        # AUTH-4: a refusal without a definite OAuth error is retried later.
        (401, True, RefreshRefused),
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
            with pytest.raises(SignInUnavailable) as caught:
                await manager.get_access_token()
            assert isinstance(caught.value.__cause__, InvalidPayload)
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


async def _rejected_refresh(
    handler,
    expected: type[RefreshTokenRejected | RefreshRefused] = RefreshTokenRejected,
) -> RefreshTokenRejected | RefreshRefused:
    runner, url = await _serve(handler)
    try:
        async with aiohttp.ClientSession() as session:
            client = OAuthClient(session, token_url=url)
            with pytest.raises(expected) as caught:
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

    error = await _rejected_refresh(handler, RefreshRefused)
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

    error = await _rejected_refresh(handler, RefreshRefused)
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


# --- C72/SEC-1: reduced scopes with a one-time fallback to the full set ---


@pytest.fixture
def scope_fallback():
    """Each test starts with the reduced set and leaves no fallback behind."""
    from custom_components.bosch_buderus_heating.pointt import auth

    auth._ScopeFallback.active = False
    yield auth._ScopeFallback
    auth._ScopeFallback.active = False


def _scopes(flow: OAuthFlow) -> list[str]:
    return parse_qs(urlparse(flow.authorization_url).query)["scope"][0].split()


def test_sign_in_asks_for_the_reduced_scope_set_first(scope_fallback) -> None:
    flow = OAuthFlow.create(Brand.BOSCH, now=100.0)
    assert _scopes(flow) == list(OAUTH_SCOPES_REDUCED)
    for unused in (
        "pointt.gateway.claiming",
        "pointt.gateway.removal",
        "pointt.gateway.users",
        "pointt.castt.flow.token-exchange",
        "hcc.tariff.read",
    ):
        assert unused not in _scopes(flow)
    assert set(OAUTH_SCOPES_REDUCED) < set(OAUTH_SCOPES)


def test_rejected_reduced_scopes_fall_back_once_to_the_full_set(
    scope_fallback,
) -> None:
    flow = OAuthFlow.create(Brand.BOSCH, now=100.0)
    old_state = flow.context.state
    rejected = f"{Brand.BOSCH.redirect_uri}?error=invalid_scope&state={old_state}"
    with pytest.raises(OAuthScopeRejected):
        flow.consume_redirect(rejected, now=101.0)
    # The same dialog now shows a fresh link with the full set.
    assert flow.scopes == OAUTH_SCOPES
    assert _scopes(flow) == list(OAUTH_SCOPES)
    assert flow.context.state != old_state
    assert not flow.is_expired(now=102.0)
    # The old attempt cannot be used any more; the new one can.
    with pytest.raises(OAuthStateMismatch):
        flow.consume_redirect(
            f"{Brand.BOSCH.redirect_uri}?code=c&state={old_state}", now=102.0
        )
    grant = flow.consume_redirect(
        f"{Brand.BOSCH.redirect_uri}?code=c&state={flow.context.state}", now=103.0
    )
    assert grant.code == "c"
    # Later attempts of this process start with the full set at once.
    later = OAuthFlow.create(Brand.BUDERUS, now=200.0)
    assert _scopes(later) == list(OAUTH_SCOPES)
    # Only once: a rejection of the full set is an ordinary OAuth error.
    with pytest.raises(OAuthRedirectError) as caught:
        later.consume_redirect(
            f"{Brand.BUDERUS.redirect_uri}?error=invalid_scope"
            f"&state={later.context.state}",
            now=201.0,
        )
    assert not isinstance(caught.value, OAuthScopeRejected)


@pytest.mark.parametrize(
    "query",
    ["error=invalid_scope&state=foreign", "error=access_denied&state={state}"],
)
def test_other_errors_or_foreign_attempts_keep_the_reduced_set(
    scope_fallback, query: str
) -> None:
    flow = OAuthFlow.create(Brand.BOSCH, now=100.0)
    state = flow.context.state
    redirect = f"{Brand.BOSCH.redirect_uri}?{query.format(state=state)}"
    with pytest.raises(OAuthRedirectError) as caught:
        flow.consume_redirect(redirect, now=101.0)
    assert not isinstance(caught.value, OAuthScopeRejected)
    assert flow.scopes == OAUTH_SCOPES_REDUCED
    assert flow.context.state == state
    assert not scope_fallback.active
    assert _scopes(OAuthFlow.create(Brand.BOSCH)) == list(OAUTH_SCOPES_REDUCED)


# --- C74/SEC-3: a malformed redirect is an invalid redirect, not a crash ---


@pytest.mark.parametrize(
    "redirect",
    [
        "com.bosch.tt.dashtt.pointt://[app/login?code=c&state=s",
        "com.bosch.tt.dashtt.pointt://app]/login?code=c&state=s",
    ],
)
def test_malformed_redirect_is_reported_as_invalid_redirect(redirect: str) -> None:
    with pytest.raises(OAuthRedirectError, match="parsed"):
        parse_redirect_url(Brand.BOSCH, redirect, expected_state="s")
    flow = OAuthFlow.create(Brand.BOSCH, now=100.0)
    with pytest.raises(OAuthRedirectError, match="parsed"):
        flow.consume_redirect(redirect, now=101.0)
    # Not consumed: the user can paste the correct address afterwards.
    valid = f"{Brand.BOSCH.redirect_uri}?code=c&state={flow.context.state}"
    assert flow.consume_redirect(valid, now=102.0).code == "c"


# --- C18/AUTH-5, SEC-5: no redirects, bounded token answers ---


@pytest.mark.parametrize("status", [302, 307, 308])
@pytest.mark.parametrize("refresh", [True, False])
async def test_token_request_never_follows_a_redirect(
    status: int, refresh: bool
) -> None:
    received: list[str] = []

    async def target(request: web.Request) -> web.Response:
        received.append(await request.text())
        return web.json_response({"access_token": "foreign", "expires_in": 3600})

    target_runner, target_url = await _serve(target)

    async def redirecting(_request: web.Request) -> web.Response:
        return web.Response(status=status, headers={"Location": target_url})

    runner, url = await _serve(redirecting)
    try:
        async with aiohttp.ClientSession() as session:
            client = OAuthClient(session, token_url=url)
            with pytest.raises(UnexpectedHttpStatus) as caught:
                if refresh:
                    await client.refresh("secret-refresh")
                else:
                    await client.exchange_code(
                        Brand.BOSCH, AuthorizationGrant("code", "secret-verifier")
                    )
    finally:
        await runner.cleanup()
        await target_runner.cleanup()
    assert caught.value.status == status
    assert received == []


async def test_oversized_token_answer_is_rejected() -> None:
    async def handler(_request: web.Request) -> web.Response:
        body = json.dumps(
            {"access_token": "a", "expires_in": 60, "padding": "x" * 70_000}
        )
        return web.Response(text=body, content_type="application/json")

    runner, url = await _serve(handler)
    try:
        async with aiohttp.ClientSession() as session:
            with pytest.raises(InvalidPayload, match="size limit"):
                await OAuthClient(session, token_url=url).refresh("r")
    finally:
        await runner.cleanup()


@pytest.mark.parametrize(
    "body", [b"[" * 60_000, b'{"access_token": "a", "expires_in": 1' + b"0" * 5000]
)
async def test_pathological_token_json_is_an_invalid_answer(body: bytes) -> None:
    async def handler(_request: web.Request) -> web.Response:
        return web.Response(body=body, content_type="application/json")

    runner, url = await _serve(handler)
    try:
        async with aiohttp.ClientSession() as session:
            with pytest.raises(InvalidPayload):
                await OAuthClient(session, token_url=url).refresh("r")
    finally:
        await runner.cleanup()


# --- C19/AUTH-6: a rejection sent in several chunks keeps its OAuth code ---


async def test_chunked_rejection_keeps_the_oauth_error() -> None:
    async def handler(request: web.Request) -> web.StreamResponse:
        response = web.StreamResponse(
            status=400, headers={"Content-Type": "application/json"}
        )
        response.enable_chunked_encoding()
        await response.prepare(request)
        await response.write(b'{"error":')
        await asyncio.sleep(0.05)
        await response.write(b'"invalid_grant"}')
        await response.write_eof()
        return response

    error = await _rejected_refresh(handler)
    assert error.rejection is not None
    assert error.rejection.body_format == "json"
    assert error.rejection.oauth_error == "invalid_grant"


async def test_long_rejection_body_is_cut_at_the_limit() -> None:
    async def handler(_request: web.Request) -> web.Response:
        return web.Response(status=403, text="<html>" + "x" * 10_000)

    error = await _rejected_refresh(handler, RefreshRefused)
    assert error.rejection is not None
    assert error.rejection.body_format == "non_json"


# --- C17/AUTH-4: only a definite OAuth error ends the sign-in ---


@pytest.mark.parametrize(
    "code",
    [
        "invalid_grant",
        "invalid_client",
        "unauthorized_client",
        "unsupported_grant_type",
    ],
)
async def test_definite_oauth_errors_reject_the_refresh_token(code: str) -> None:
    async def handler(_request: web.Request) -> web.Response:
        return web.json_response({"error": code}, status=400)

    error = await _rejected_refresh(handler)
    assert isinstance(error, RefreshTokenRejected)
    assert error.rejection is not None
    assert error.rejection.oauth_error == code


@pytest.mark.parametrize(
    ("status", "body", "content_type"),
    [
        (403, "<html>Blocked by a firewall</html>", "text/html"),
        (401, "", "text/plain"),
        (400, '{"error": "invalid_request"}', "application/json"),
        (400, '{"error": "temporarily_unavailable"}', "application/json"),
    ],
)
async def test_refusal_without_definite_error_keeps_the_sign_in(
    status: int, body: str, content_type: str
) -> None:
    calls = 0

    async def handler(_request: web.Request) -> web.Response:
        nonlocal calls
        calls += 1
        return web.Response(status=status, text=body, content_type=content_type)

    runner, url = await _serve(handler)
    clock = [0.0]
    try:
        async with aiohttp.ClientSession() as session:
            manager = TokenManager(
                OAuthClient(session, token_url=url),
                AuthTokens("old", "refresh", 0.0),
                lambda tokens: None,
                retry_clock=lambda: clock[0],
            )
            with pytest.raises(SignInUnavailable) as caught:
                await manager.get_access_token()
            assert isinstance(caught.value.__cause__, RefreshRefused)
            assert caught.value.rejection is not None
            assert caught.value.rejection.http_status == status
            assert manager.refresh_rejected is False
            # Retried after the wait instead of asking for a new sign-in.
            clock[0] = 31.0
            with pytest.raises(SignInUnavailable):
                await manager.get_access_token()
    finally:
        await runner.cleanup()
    assert calls == 2
