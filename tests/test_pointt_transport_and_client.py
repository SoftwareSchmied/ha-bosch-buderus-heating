from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import aiohttp
import pytest
from aiohttp import web

from custom_components.bosch_buderus_heating.pointt import (
    InvalidPayload,
    PointTClient,
    RateLimited,
    ResourceForbidden,
    ResourceNotAcceptable,
    ResourceNotFound,
    RetryPolicy,
    ServiceUnavailable,
    UnexpectedHttpStatus,
)
from custom_components.bosch_buderus_heating.pointt.const import DEFAULT_USER_AGENT
from custom_components.bosch_buderus_heating.pointt.models import AuthTokens
from custom_components.bosch_buderus_heating.pointt.transport import (
    PointTTransport,
    _validate_json,
)

pytestmark = pytest.mark.usefixtures("socket_enabled")

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


@asynccontextmanager
async def serve(routes: list[tuple[str, str, Handler]]) -> AsyncIterator[str]:
    app = web.Application()
    for method, path, handler in routes:
        app.router.add_route(method, path, handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    assert site._server is not None
    port = site._server.sockets[0].getsockname()[1]
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        await runner.cleanup()


class RotatingProvider:
    def __init__(self) -> None:
        self.calls: list[bool] = []

    async def get_access_token(self, *, force_refresh: bool = False) -> str:
        self.calls.append(force_refresh)
        return "fresh" if force_refresh else "stale"


async def test_client_reads_all_supported_endpoints_and_encodes_paths() -> None:
    requests: list[tuple[str, str, str, str, str]] = []

    async def gateways(request: web.Request) -> web.Response:
        requests.append(
            (
                request.method,
                request.path,
                request.headers["Authorization"],
                request.headers["Content-Type"],
                request.headers["User-Agent"],
            )
        )
        return web.json_response([{"id": "gw one", "deviceType": "heatpump"}])

    async def gateway(request: web.Request) -> web.Response:
        return web.json_response({"id": "gw one", "model": "Compress"})

    async def part_number(request: web.Request) -> web.Response:
        return web.json_response({"partNumber": "PN-1"})

    async def resource(request: web.Request) -> web.Response:
        assert request.match_info["tail"] == "heat/set point"
        return web.json_response({"value": 20.5, "unitOfMeasure": "C"})

    bulk_bodies: list[object] = []

    async def bulk(request: web.Request) -> web.Response:
        body = await request.json()
        bulk_bodies.append(body)
        paths = body[0]["resourcePaths"]
        return web.json_response(
            [
                {
                    "gatewayId": "gw one",
                    "resourcePaths": [
                        {
                            "resourcePath": path,
                            "serverStatus": 200,
                            "gatewayResponse": {
                                "status": 200,
                                "payload": {"value": path},
                            },
                        }
                        for path in paths
                    ],
                }
            ]
        )

    routes = [
        ("GET", "/gateways/", gateways),
        ("GET", "/gateways/{id}/partnumber", part_number),
        ("GET", "/gateways/{id}/resource/{tail:.*}", resource),
        ("GET", "/gateways/{id}", gateway),
        ("POST", "/bulk", bulk),
    ]
    async with serve(routes) as url, aiohttp.ClientSession() as session:
        client = PointTClient(session, "token", base_url=url)
        assert (await client.get_gateways())[0].gateway_id == "gw one"
        assert (await client.get_gateway("gw one")).model == "Compress"
        assert await client.get_part_number("gw one") == "PN-1"
        assert (await client.get_resource("gw one", "/heat/set point")).value == 20.5
        results = await client.get_resources_bulk(
            "gw one", [f"/r/{index}" for index in range(3)], chunk_size=2
        )

    assert all(item.ok for item in results)
    assert len(bulk_bodies) == 2
    metrics = client.metrics.snapshot()
    assert metrics["requests_total"] == 6
    assert metrics["requests_by_category"] == {
        "bulk": 2,
        "gateway_list": 1,
        "gateway_metadata": 2,
        "resource": 1,
    }
    assert metrics["bulk_resource_paths_total"] == 3
    assert metrics["bulk_items_successful"] == 3
    assert metrics["maximum_bulk_size"] == 2
    assert requests == [
        (
            "GET",
            "/gateways/",
            "Bearer token",
            "application/json",
            DEFAULT_USER_AGENT,
        )
    ]


async def test_client_retries_once_with_forced_token_refresh() -> None:
    seen: list[str] = []

    async def handler(request: web.Request) -> web.Response:
        authorization = request.headers["Authorization"]
        seen.append(authorization)
        if authorization == "Bearer stale":
            return web.Response(status=401)
        return web.json_response([])

    provider = RotatingProvider()
    async with (
        serve([("GET", "/gateways/", handler)]) as url,
        aiohttp.ClientSession() as session,
    ):
        client = PointTClient(session, provider, base_url=url)
        assert await client.get_gateways() == ()

    assert provider.calls == [False, True]
    assert seen == ["Bearer stale", "Bearer fresh"]


async def test_client_writes_value_once_and_parses_optional_echo() -> None:
    bodies: list[object] = []

    async def write(request: web.Request) -> web.Response:
        bodies.append(await request.json())
        if len(bodies) == 1:
            return web.Response(status=204)
        return web.json_response({"value": "auto"})

    async with (
        serve([("PUT", "/gateways/{id}/resource/{tail:.*}", write)]) as url,
        aiohttp.ClientSession() as session,
    ):
        client = PointTClient(session, "token", base_url=url)
        assert await client.put_resource_value("gw one", "/mode", "manual") is None
        echoed = await client.put_resource_value("gw one", "/mode", "auto")

    assert echoed is not None and echoed.value == "auto"
    assert bodies == [{"value": "manual"}, {"value": "auto"}]
    assert client.metrics.snapshot()["requests_by_method"] == {"PUT": 2}


async def test_transport_retries_temporary_service_failure() -> None:
    calls = 0
    sleeps: list[float] = []

    async def handler(_request: web.Request) -> web.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return web.Response(status=503)
        return web.json_response({"ok": True})

    async def sleep(delay: float) -> None:
        sleeps.append(delay)

    async with (
        serve([("GET", "/data", handler)]) as url,
        aiohttp.ClientSession() as session,
    ):
        transport = PointTTransport(
            session,
            base_url=url,
            retry_policy=RetryPolicy(attempts=2, base_delay=0.1),
            sleep=sleep,
            random_value=lambda: 0.0,
        )
        assert await transport.request_json("GET", "data", "token", retryable=True) == {
            "ok": True
        }

    assert calls == 2
    assert sleeps == [0.1]
    assert transport.metrics.snapshot()["retry_attempts"] == 1
    assert transport.metrics.snapshot()["outcomes"] == {
        "service_unavailable": 1,
        "success": 1,
    }


@pytest.mark.parametrize(
    ("status", "exception"),
    [
        (403, ResourceForbidden),
        (404, ResourceNotFound),
        (406, ResourceNotAcceptable),
        (418, UnexpectedHttpStatus),
        (500, ServiceUnavailable),
    ],
)
async def test_transport_maps_http_statuses(
    status: int, exception: type[Exception]
) -> None:
    async def handler(_request: web.Request) -> web.Response:
        return web.Response(status=status, text="private response")

    async with (
        serve([("GET", "/data", handler)]) as url,
        aiohttp.ClientSession() as session,
    ):
        transport = PointTTransport(session, base_url=url, retry_policy=RetryPolicy(1))
        with pytest.raises(exception) as caught:
            await transport.request_json(
                "GET", "data", "token", resource_path="/heat/private"
            )
    assert "private response" not in str(caught.value)


async def test_transport_exposes_numeric_retry_after() -> None:
    async def handler(_request: web.Request) -> web.Response:
        return web.Response(status=429, headers={"Retry-After": "2.5"})

    async with (
        serve([("GET", "/data", handler)]) as url,
        aiohttp.ClientSession() as session,
    ):
        with pytest.raises(RateLimited) as caught:
            await PointTTransport(session, base_url=url).request_json(
                "GET", "data", "token"
            )
    assert caught.value.retry_after == 2.5


async def test_transport_handles_empty_responses_and_invalid_retry_after() -> None:
    async def no_content(_request: web.Request) -> web.Response:
        return web.Response(status=204)

    async def empty(_request: web.Request) -> web.Response:
        return web.Response(body=b"")

    async def limited(_request: web.Request) -> web.Response:
        return web.Response(status=429, headers={"Retry-After": "later"})

    routes = [
        ("GET", "/none", no_content),
        ("GET", "/empty", empty),
        ("GET", "/limited", limited),
    ]
    async with serve(routes) as url, aiohttp.ClientSession() as session:
        transport = PointTTransport(session, base_url=url)
        assert await transport.request_json("GET", "none", "token") is None
        assert await transport.request_json("GET", "empty", "token") is None
        with pytest.raises(RateLimited) as caught:
            await transport.request_json("GET", "limited", "token")
    assert caught.value.retry_after is None


@pytest.mark.parametrize(
    ("body", "content_type"),
    [(b"not-json", "application/json"), (b'{"bad": NaN}', "application/json")],
)
async def test_transport_rejects_invalid_json(body: bytes, content_type: str) -> None:
    async def handler(_request: web.Request) -> web.Response:
        return web.Response(body=body, content_type=content_type)

    async with (
        serve([("GET", "/data", handler)]) as url,
        aiohttp.ClientSession() as session,
    ):
        with pytest.raises(InvalidPayload):
            await PointTTransport(session, base_url=url).request_json(
                "GET", "data", "token"
            )


def test_configuration_validation() -> None:
    with pytest.raises(ValueError):
        RetryPolicy(0)
    session = object()
    with pytest.raises(ValueError):
        PointTTransport(session, concurrency=0)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [{1: "not a JSON object"}, object()])
def test_json_validator_rejects_unsupported_python_values(value: object) -> None:
    with pytest.raises(InvalidPayload):
        _validate_json(value)


async def test_client_rejects_blank_gateway_id() -> None:
    async with aiohttp.ClientSession() as session:
        client = PointTClient(session, "token", base_url="http://127.0.0.1")
        with pytest.raises(ValueError):
            await client.get_gateway("  ")


def test_auth_tokens_expiry_margin() -> None:
    tokens = AuthTokens("token", expires_at=1100.0)
    assert not tokens.is_expired(now=1000.0, margin=50.0)
    assert tokens.is_expired(now=1050.0, margin=50.0)
