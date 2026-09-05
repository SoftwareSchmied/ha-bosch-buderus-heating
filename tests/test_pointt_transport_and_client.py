from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
from custom_components.bosch_buderus_heating.pointt.const import (
    DEFAULT_USER_AGENT,
    MAX_RESPONSE_BYTES,
)
from custom_components.bosch_buderus_heating.pointt.models import AuthTokens
from custom_components.bosch_buderus_heating.pointt.transport import (
    PointTTransport,
    RateLimitBackoff,
    _read_limited,
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


@pytest.mark.parametrize(
    "delay, expected",
    [(None, 300), (0, 60), (float("inf"), 300), (99999, 3600), (90, 90)],
)
def test_account_backoff_is_bounded_and_expires(delay, expected) -> None:
    now = 10.0
    backoff = RateLimitBackoff(clock=lambda: now)
    backoff.activate(delay)
    with pytest.raises(RateLimited) as raised:
        backoff.raise_if_active()
    assert raised.value.retry_after == expected
    backoff.activate(0)
    now += expected
    backoff.raise_if_active()


async def test_http_rate_limit_blocks_other_gateways_and_queued_requests() -> None:
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def limited(request: web.Request) -> web.Response:
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return web.Response(status=429, headers={"Retry-After": "90"})

    async with (
        serve([("GET", "/gateways/{id}/resource/{tail:.*}", limited)]) as url,
        aiohttp.ClientSession() as session,
    ):
        client = PointTClient(session, "token", base_url=url, concurrency=1)
        first = asyncio.create_task(client.get_resource("gateway-one", "/system"))
        await entered.wait()
        queued = asyncio.create_task(client.get_resource("gateway-two", "/system"))
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(first, queued, return_exceptions=True)
        assert all(isinstance(result, RateLimited) for result in results)
        with pytest.raises(RateLimited):
            await client.put_resource_value("gateway-two", "/system", "on")
        assert calls == 1


async def test_bulk_item_rate_limit_stops_remaining_chunks_and_account_requests() -> (
    None
):
    calls = 0

    async def limited(request: web.Request) -> web.Response:
        nonlocal calls
        calls += 1
        return web.json_response(
            [
                {
                    "gatewayId": "gateway-one",
                    "resourcePaths": [{"resourcePath": "/system", "serverStatus": 429}],
                }
            ]
        )

    async with (
        serve([("POST", "/bulk", limited)]) as url,
        aiohttp.ClientSession() as session,
    ):
        client = PointTClient(session, "token", base_url=url)
        results = await client.get_resources_bulk(
            "gateway-one", ["/system", "/gateway"], chunk_size=1
        )
        assert results[0].status == 429
        assert len(results) == 1
        with pytest.raises(RateLimited):
            await client.get_resource("gateway-two", "/system")
        assert calls == 1


@pytest.mark.parametrize("size", [MAX_RESPONSE_BYTES, MAX_RESPONSE_BYTES + 100_000])
async def test_response_body_is_bounded_during_read(size: int) -> None:
    consumed = 0

    async def read(limit: int) -> bytes:
        nonlocal consumed
        count = min(limit, size - consumed, 7000)
        consumed += count
        return b"x" * count

    response = SimpleNamespace(
        content=SimpleNamespace(read=AsyncMock(side_effect=read))
    )
    if size > MAX_RESPONSE_BYTES:
        with pytest.raises(InvalidPayload):
            await _read_limited(response)
        assert consumed == MAX_RESPONSE_BYTES + 1
    else:
        assert len(await _read_limited(response)) == size


async def test_simultaneous_401_responses_refresh_the_account_once() -> None:
    from custom_components.bosch_buderus_heating.pointt import TokenManager

    count = 0
    entered = asyncio.Event()

    async def handler(request: web.Request) -> web.Response:
        nonlocal count
        if request.headers["Authorization"] == "Bearer stale":
            count += 1
            if count == 3:
                entered.set()
            await entered.wait()
            return web.Response(status=401)
        return web.json_response({"value": 1})

    oauth = SimpleNamespace(
        refresh=AsyncMock(
            return_value=AuthTokens("fresh", "rotated", expires_at=4_000_000_000)
        )
    )
    manager = TokenManager(
        oauth,
        AuthTokens("stale", "refresh", expires_at=4_000_000_000),
        lambda tokens: None,
    )
    async with (
        serve([("GET", "/gateways/{id}/resource/system", handler)]) as url,
        aiohttp.ClientSession() as session,
    ):
        client = PointTClient(session, manager, base_url=url)
        results = await asyncio.gather(
            *(client.get_resource(f"gateway-{n}", "/system") for n in range(3))
        )
    assert [item.value for item in results] == [1, 1, 1]
    oauth.refresh.assert_awaited_once()


class RotatingProvider:
    def __init__(self) -> None:
        self.calls: list[bool] = []

    async def get_access_token(
        self, *, force_refresh: bool = False, rejected_token: str | None = None
    ) -> str:
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
    bulk_events = [
        event for event in metrics["recent_requests"] if event["type"] == "bulk"
    ]
    assert [event["bulk_size"] for event in bulk_events] == [2, 1]
    assert [event["bulk_items_successful"] for event in bulk_events] == [2, 1]
    assert all(
        event["bulk_server_statuses"] == {"200": event["bulk_size"]}
        for event in bulk_events
    )
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
    metrics = client.metrics.snapshot()
    assert metrics["retry_attempts"] == 1
    assert [event["http_status"] for event in metrics["recent_requests"]] == [
        401,
        200,
    ]
    assert metrics["recent_requests"][1]["attempt"] == 2
    assert metrics["recent_requests"][1]["retry"] is True


async def test_client_marks_individual_bulk_fallback_without_exposing_path() -> None:
    async def handler(_request: web.Request) -> web.Response:
        return web.json_response({"value": 20.5})

    async with (
        serve([("GET", "/gateways/{id}/resource/{tail:.*}", handler)]) as url,
        aiohttp.ClientSession() as session,
    ):
        client = PointTClient(session, "token", base_url=url)
        await client.get_resource(
            "private-gateway", "/private/resource", fallback_reason="gateway_5xx"
        )

    metrics = client.metrics.snapshot()
    assert metrics["fallback_requests"] == 1
    assert metrics["fallback_requests_by_reason"] == {"gateway_5xx": 1}
    assert metrics["rolling_60_minutes"]["requests_by_type"] == {"fallback": 1}
    event = metrics["recent_requests"][0]
    assert event["fallback_reason"] == "gateway_5xx"
    assert "private" not in repr(event)


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


async def test_client_uses_exact_holiday_endpoints_and_unwrapped_list_body() -> None:
    requests: list[tuple[str, str, object | None]] = []

    async def holiday(request: web.Request) -> web.Response:
        body = await request.json() if request.can_read_body else None
        requests.append((request.method, request.path, body))
        return web.Response(status=204)

    values = {
        "startDate": "2030-08-01T08:00:00",
        "endDate": "2030-08-08T18:00:00",
        "heatingMode": "FIX_TEMPERATURE",
        "dhwMode": "OFF",
        "ventilationMode": None,
        "assignedTo": ["hc1", "dhw1"],
        "name": "VGVzdA==",
        "thermalDesinfection": "ON",
        "fixTemperature": 17.0,
    }
    routes = [
        ("POST", "/gateways/{id}/resource/holidayMode", holiday),
        ("PUT", "/gateways/{id}/resource/holidayMode/{holiday_id}", holiday),
        ("DELETE", "/gateways/{id}/resource/holidayMode/{holiday_id}", holiday),
    ]
    async with serve(routes) as url, aiohttp.ClientSession() as session:
        client = PointTClient(session, "token", base_url=url)
        await client.create_holiday_period("gw one", values)
        await client.update_holiday_period("gw one", 7, values)
        await client.delete_holiday_period("gw one", 7)
        with pytest.raises(ValueError):
            await client.update_holiday_period("gw one", True, values)  # type: ignore[arg-type]

    assert requests == [
        ("POST", "/gateways/gw one/resource/holidayMode", [values]),
        ("PUT", "/gateways/gw one/resource/holidayMode/7", [values]),
        ("DELETE", "/gateways/gw one/resource/holidayMode/7", None),
    ]
    assert client.metrics.snapshot()["requests_by_method"] == {
        "DELETE": 1,
        "POST": 1,
        "PUT": 1,
    }


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
    recent = transport.metrics.snapshot()["recent_requests"]
    assert [event["attempt"] for event in recent] == [1, 2]
    assert [event["retry"] for event in recent] == [False, True]
    assert [event["http_status"] for event in recent] == [503, 200]


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
