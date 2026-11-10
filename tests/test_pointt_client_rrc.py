"""W0: the client calls of the RRC packages over real HTTP.

Structured PUT, POST and DELETE are sent once and never retried; query
reads accept only ``entry``, ``date`` and ``next``; errors and metrics name
the path without the query.
"""

from __future__ import annotations

import logging

import aiohttp
import pytest
from aiohttp import web

from custom_components.bosch_buderus_heating.pointt import (
    PointTClient,
    RateLimited,
    ResourceNotFound,
    RetryPolicy,
    ServiceUnavailable,
)
from custom_components.bosch_buderus_heating.pointt.bulk import normalize_query
from tests.test_pointt_transport_and_client import serve

pytestmark = pytest.mark.usefixtures("socket_enabled")
NO_WAIT = RetryPolicy(attempts=3, base_delay=0, maximum_delay=0)


async def test_structured_put_post_and_delete_are_sent_once() -> None:
    requests: list[tuple[str, str, object | None]] = []

    async def handle(request: web.Request) -> web.Response:
        body = await request.json() if request.can_read_body else None
        requests.append((request.method, request.path, body))
        return web.Response(status=204)

    routes = [
        (method, "/gateways/{id}/resource/{tail:.*}", handle)
        for method in ("PUT", "POST", "DELETE")
    ]
    boost = [{"temperature": 21.5, "duration": 2, "mode": "on", "zones": [1]}]
    event = [{"name": "QQ==", "start": "2030-01-02", "end": "2030-01-03"}]
    async with serve(routes) as url, aiohttp.ClientSession() as session:
        client = PointTClient(session, "token", base_url=url)
        assert (
            await client.put_resource_json(
                "gw one", "/heatingCircuits/hc1/boostShortcut", boost
            )
            is None
        )
        assert await client.post_resource("gw one", "/events", event) is None
        await client.delete_resource("gw one", "/events/ev3")
        with pytest.raises(ValueError):
            await client.put_resource_json("gw one", "/x", "scalar")
        with pytest.raises(ValueError):
            await client.delete_resource("gw one", "/events/../gateway")

    assert requests == [
        (
            "PUT",
            "/gateways/gw one/resource/heatingCircuits/hc1/boostShortcut",
            {"value": boost},
        ),
        ("POST", "/gateways/gw one/resource/events", {"value": event}),
        ("DELETE", "/gateways/gw one/resource/events/ev3", None),
    ]
    assert client.metrics.snapshot()["requests_by_method"] == {
        "DELETE": 1,
        "POST": 1,
        "PUT": 1,
    }


@pytest.mark.parametrize("method", ["POST", "DELETE", "PUT"])
async def test_writes_never_retry_a_server_failure(method: str) -> None:
    calls = 0

    async def fail(request: web.Request) -> web.Response:
        nonlocal calls
        calls += 1
        return web.Response(status=503)

    route = [(method, "/gateways/{id}/resource/{tail:.*}", fail)]
    async with serve(route) as url, aiohttp.ClientSession() as session:
        client = PointTClient(session, "token", base_url=url, retry_policy=NO_WAIT)
        with pytest.raises(ServiceUnavailable):
            if method == "POST":
                await client.post_resource("gw", "/events", [])
            elif method == "DELETE":
                await client.delete_resource("gw", "/events/ev1")
            else:
                await client.put_resource_json("gw", "/events/ev1", [])
    assert calls == 1


async def test_a_rate_limited_write_activates_the_account_brake() -> None:
    async def limited(request: web.Request) -> web.Response:
        return web.Response(status=429, headers={"Retry-After": "120"})

    route = [("POST", "/gateways/{id}/resource/{tail:.*}", limited)]
    async with serve(route) as url, aiohttp.ClientSession() as session:
        client = PointTClient(session, "token", base_url=url)
        with pytest.raises(RateLimited) as raised:
            await client.post_resource("gw", "/events", [])
        assert raised.value.retry_after == pytest.approx(120, abs=2)
        assert client.backoff.remaining > 100
        with pytest.raises(RateLimited):
            await client.delete_resource("gw", "/events/ev1")


async def test_query_reads_send_only_validated_keys(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="custom_components.bosch_buderus_heating")
    seen: list[tuple[str, dict[str, str]]] = []

    async def history(request: web.Request) -> web.Response:
        seen.append((request.path, dict(request.query)))
        if request.query.get("entry") == "9":
            return web.Response(status=404)
        return web.json_response(
            {
                "id": "/energy/history",
                "type": "energyRecordings",
                "value": [{"d": "01-01-2030", "T": 4.5}],
            }
        )

    route = [("GET", "/gateways/{id}/resource/{tail:.*}", history)]
    async with serve(route) as url, aiohttp.ClientSession() as session:
        client = PointTClient(session, "token", base_url=url)
        by_entry = await client.get_resource_query(
            "gw", "/energy/history", {"entry": 3}
        )
        by_date = await client.get_resource_query(
            "gw", "/energy/history", {"date": "2030-02-28"}
        )
        with pytest.raises(ResourceNotFound) as missing:
            await client.get_resource_query("gw", "/energy/history", {"entry": 9})
        for query in (
            {},
            {"page": 1},
            {"entry": -1},
            {"entry": True},
            {"entry": "3"},
            {"next": 10**7},
            {"date": "2030-02-30"},
            {"date": "30-01-2030"},
            {"date": "2030-01-01&x=1"},
            {"date": 20300101},
        ):
            with pytest.raises(ValueError):
                await client.get_resource_query("gw", "/energy/history", query)
        with pytest.raises(ValueError):
            await client.get_resource_query("gw", "/energy/history?x=1", {"next": 1})

    assert by_entry.path == by_date.path == "/energy/history"
    assert by_entry.value == [{"d": "01-01-2030", "T": 4.5}]
    assert seen == [
        ("/gateways/gw/resource/energy/history", {"entry": "3"}),
        ("/gateways/gw/resource/energy/history", {"date": "2030-02-28"}),
        ("/gateways/gw/resource/energy/history", {"entry": "9"}),
    ]
    # The error, the metrics and the log name the path, never the query.
    assert "?" not in str(missing.value) and "entry" not in str(missing.value)
    snapshot = str(client.metrics.snapshot())
    assert "entry" not in snapshot and "2030" not in snapshot
    ours = [
        record.getMessage()
        for record in caplog.records
        if record.name.startswith("custom_components")
    ]
    assert not any("2030-02-28" in message for message in ours)


def test_query_normalisation_is_sorted_text() -> None:
    assert normalize_query({"next": 2, "date": "2030-01-01"}) == (
        ("date", "2030-01-01"),
        ("next", "2"),
    )
